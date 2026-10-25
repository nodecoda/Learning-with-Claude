# Ch 16 — 观察 RegBankSelect：MIR 与 verifier

> **目标**：把 RegBankSelect 变成“可观察”的一站。学完你能用 `-stop-after=regbankselect`、`-run-pass=regbankselect`、`-print-after`、`-run-pass=machineverifier` 和 `-global-isel-abort` 回答三个问题：pass 跑了吗？每个 vreg 在哪个 bank？失败时失败在哪条指令？
>
> **环境**：LLVM 23.1.4，Windows x86_64 官方预编译包（Optimized build，实测）；源码构建用 assertions 版本（guided）。主 target AArch64。
>
> **证据约定**：本章的 MIR 片段、错误串、remark 都在官方 23.1.4 包上实际跑过，标“实测”；只从源码推出、没有在当前环境复现的，标“源码依据”或 `guided`。不伪造 lit 或构建输出。

## 为什么需要这个？

`llc` 默认只给你最终汇编。汇编正确，不代表 RegBankSelect 做对了：

- 它可能插入三条本可以避免的跨 bank COPY，但汇编仍然正确。
- 它可能在某条指令上失败，然后 pipeline 悄悄 fallback 到 SelectionDAG；最终汇编看起来完全正常。
- 它可能在 `-global-isel-abort=0` 下把函数标成 `regBankSelected: true`，同时留下没有 bank 的活 vreg；只看汇编永远发现不了。

本章建立“中间状态的可观察性”。没有这层能力，Ch 17 的 Fast/Greedy 对比、Ch 18 的失败诊断、Ch 19 的测试都会变成猜。一个最小的反例（实测）：`regBankSelected: true`、`failedISel: true`，以及三个 `class: _` 的活 vreg；完整 MIR 见实验三。

## 先建立直觉

把 GlobalISel 想成机场安检线：IRTranslator 送旅客进楼，Legalizer 查证件，RegBankSelect 分航站楼，InstructionSelect 排航班。

```text
LLVM IR
  │  -stop-after=regbankselect         ← 从 IR 开始，停在 RBS 之后
  ▼
legal gMIR
  │  -run-pass=regbankselect           ← 只从 MIR 重放 RBS 这一站
  ▼
banked gMIR
  │  -print-after=regbankselect        ← 在 RBS 之后拍快照（stderr）
  │  -verify-machineinstrs             ← 每站之后验一遍
  ▼
InstructionSelect
  │  -global-isel-abort=0/1/2          ← 静默 fallback / 立刻中止 / 打 remark
  ▼
target MIR
```

四个 flag 的分工：

| Flag | 输入 | 作用 | 输出去哪 |
| --- | --- | --- | --- |
| `-stop-after=regbankselect` | IR 或 MIR | 跑完整管线到 RBS 后停止 | `-o` 文件 |
| `-run-pass=regbankselect` | **只能 MIR** | 只跑 RBS | `-o` 文件 |
| `-print-after=regbankselect` | IR 或 MIR | pass 结束后打印 machine dump | **stderr** |
| `-verify-machineinstrs` | IR 或 MIR | 每个 machine pass 后跑 MachineVerifier | stderr |

`-print-after` 不是 `-o` 的替代品。PowerShell 里必须显式 `2>` 重定向，否则 dump 会混在终端输出里，而 `-o` 文件只有正常 MIR。

## 核心概念

### pass 契约写在 header 里

`RegBankSelect.h` 用三个属性声明契约（源码依据）：

```cpp
MachineFunctionProperties getRequiredProperties() const override {
  return MachineFunctionProperties().setIsSSA().setLegalized();
}
MachineFunctionProperties getSetProperties() const override {
  return MachineFunctionProperties().setRegBankSelected();
}
MachineFunctionProperties getClearedProperties() const override {
  return MachineFunctionProperties().setNoPHIs();
}
```

| MIR 属性 | 谁负责 | 意味着什么 |
| --- | --- | --- |
| `isSSA: true` | IRTranslator / Legalizer | 仍是 SSA |
| `legalized: true` | Legalizer | RBS 的输入前提 |
| `regBankSelected: true` | RegBankSelect | pass manager 认为 RBS 跑过了 |
| `selected: false` | InstructionSelect | 还没选具体指令 |
| `failedISel: true` | `reportGISelFailure` | 函数已失败，可能 fallback |

关键：`regBankSelected` 是 pass manager 根据 `getSetProperties()` 打的，不是“每条 vreg 都成功拿到 bank”的证明。失败路径也会置它，同时置 `failedISel`。

### MIR 里的 bank 名字

AArch64 的三个 bank 在 `AArch64RegisterBanks.td` 里叫 `GPR`、`FPR`、`CC`；MIR 打印时转小写：

```yaml
registers:
  - { id: 0, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 1, class: fpr, preferred-register: '', flags: [  ] }
  - { id: 2, class: cc,  preferred-register: '', flags: [  ] }
```

| 写法 | 含义 | 出现阶段 |
| --- | --- | --- |
| `class: gpr` | **bank** 名 | RBS 之后、ISel 之前 |
| `class: gpr64` | **register class** 名 | ISel 之后 |
| `class: _` | 没有 regclass，也没有 bank | legalized MIR 或失败后 |
| `%0:gpr(i64)` | vreg 的 bank 是 GPR | banked gMIR |
| `%0:gpr64(i64)` | vreg 的 regclass 是 GPR64 | selected MIR |
| `$x0` | 物理寄存器，没有 bank | ABI 边界 |

`gpr` 与 `gpr64` 只差几个字符，但前者是 bank，后者是寄存器类。把 `gpr64` 当 bank 查 `RegisterBankInfo` 会找不到。

### `_` 到底是什么意思

`_` 不是“死 vreg”的标记，而是“没有 regclass、也没有 bank”的兜底输出（`TargetRegisterInfo.cpp`，源码依据）：

```cpp
if (RegInfo.getRegClassOrNull(Reg))
  OS << StringRef(TRI->getRegClassName(RegInfo.getRegClass(Reg))).lower();
else if (RegInfo.getRegBankOrNull(Reg))
  OS << StringRef(RegInfo.getRegBankOrNull(Reg)->getName()).lower();
else {
  OS << "_";
  assert((RegInfo.def_empty(Reg) || RegInfo.getType(Reg).isValid()) &&
         "Generic registers must have a valid type");
}
```

所以 `_` 本身不说明 vreg 是死是活。判断方法见实验三。

## 底层机制

### pass 入口：先看失败，再看 optnone

`runOnMachineFunction` 的顺序很短，但每一步都影响你看到的 MIR（源码依据）：

```cpp
if (MF.getProperties().hasFailedISel())
  return false;
if (F.hasOptNone())
  OptMode = Mode::Fast;
init(MF);
#ifndef NDEBUG
if (!checkFunctionIsLegal(MF))
  return false;
#endif
assignRegisterBanks(MF);
```

四个观察点：

1. **`hasFailedISel()` 提前返回**：前面的 pass 失败过，RBS 什么都不做，你会看到 `failedISel: true` 和一堆 `_`。
2. **`optnone` 强制 Fast**：`-regbankselect-greedy` 没生效时，先看函数有没有 `optnone`。
3. **`checkFunctionIsLegal` 在 Optimized build 里不存在**：它包在 `#ifndef NDEBUG` 里。官方 23.1.4 包是 Optimized build，所以非法 MIR 直接 `-run-pass` 不一定先报 “instruction is not legal”。
4. **`assignRegisterBanks` 的返回值被忽略**：函数最后固定 `return false`；失败靠 `reportGISelFailure` 设属性，不靠返回值。

### 失败如何变成 `failedISel`

`assignRegisterBanks` 在 `assignInstr` 返回 false 时调用（源码依据）：

```cpp
if (!assignInstr(MI)) {
  reportGISelFailure(MF, *MORE, "gisel-regbankselect",
                     "unable to map instruction", MI);
  return false;
}
```

`reportGISelFailure` 先 `MF.getProperties().setFailedISel();`，再走 `reportGISelDiagnostic`。后者根据 abort 模式决定 fatal 还是 remark：

```cpp
bool IsFatal = Severity == DS_Error && IsGlobalISelAbortEnabled;
if (IsFatal)
  reportFatalUsageError(Twine(R.getMsg()));
else
  MORE.emit(R);
```

| `-global-isel-abort` | 枚举 | 行为 | 你看到什么 |
| --- | --- | --- | --- |
| `0` | `Disable` | 不中止，设 `failedISel` | 退出码 0，`failedISel: true` + `_` |
| `1` | `Enable` | 直接 fatal | `LLVM ERROR: unable to map instruction: ...` |
| `2` | `DisableWithDiag` | 不中止，发 diagnostic | 需 `-pass-remarks-missed=gisel-regbankselect` |

### `-run-pass` 的输入契约

`-run-pass` 只接受 MIR。拿 IR 直接跑（实测）：

```text
llc.exe: error: run-pass is for .mir file only.
```

正确顺序：

```powershell
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -stop-after=legalizer or.ll -o or-legal.mir
& $llc -mtriple=aarch64-linux-gnu `
  -run-pass=regbankselect or-legal.mir -o or-rbs.mir
```

`or-legal.mir` 应是 `legalized: true`、`regBankSelected: false`。如果已经是 true，你仍可重跑（实测退出码 0、函数体不变），但测试 fixture 不要依赖这个行为。

### verifier 的“没有 bank”检查

`MachineVerifier` 在 `RegBankSelected` 函数里检查每个 generic vreg（源码依据）：

```cpp
if (!RegBank && isFunctionRegBankSelected) {
  report("Generic virtual register must have a bank in a "
         "RegBankSelected function", MO, MONum);
  return;
}
```

debug value 有一个例外：如果操作数是 debug value 的 use，且该 vreg 没有定义，检查会跳过：

```cpp
if (isFunctionTracksDebugUserValues || !MO->isUse() ||
    !MI->isDebugValue() || !MRI->def_empty(Reg)) { ... }
```

所以：**debug-only、无定义的 `_` 可以合法存在；真实指令里的 `_` 不合法。**

## 实验一：同一份 MIR，两条入口

```llvm
define float @or_f(float %a, float %b) {
  %ai = bitcast float %a to i32
  %bi = bitcast float %b to i32
  %r = or i32 %ai, %bi
  %re = bitcast i32 %r to float
  ret float %re
}
```

路径 A，从 IR 停在 RBS：

```powershell
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -global-isel-abort=1 -stop-after=regbankselect `
  or.ll -o or-stop.mir
```

路径 B，从 legalized MIR 只跑 RBS：

```powershell
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -stop-after=legalizer or.ll -o or-legal.mir
& $llc -mtriple=aarch64-linux-gnu `
  -run-pass=regbankselect or-legal.mir -o or-run.mir
```

两条路径的函数体（实测，逐行相同）：

```yaml
%0:fpr(f32) = COPY $s0
%1:fpr(f32) = COPY $s1
%2:fpr(i32) = G_BITCAST %0(f32)
%3:fpr(i32) = G_BITCAST %1(f32)
%6:gpr(i32) = COPY %2(i32)
%7:gpr(i32) = COPY %3(i32)
%4:gpr(i32) = G_OR %6, %7
%5:gpr(f32) = G_BITCAST %4(i32)
$s0 = COPY %5(f32)
RET_ReallyLR implicit $s0
```

但两个**文件**不是逐字节相同（实测 SHA-256 不同）。差异只在：

```text
; ModuleID = 'or.ll'          vs  ; ModuleID = 'or-legal.mir'
maxCallFrameSize: 4294967295   vs  maxCallFrameSize: 0
bb.1 (%ir-block.0)             vs  bb.0 (%ir-block.0)
```

比较时看函数体，不要对整文件做 hash。Ch 0 说“两条路径输出应该一致”，指的是 bank 结果，不是字节级文件。

## 实验二：把 `-print-after` 抓到文件

```powershell
& $llc -mtriple=aarch64-linux-gnu -run-pass=regbankselect `
  -print-after=regbankselect or-legal.mir `
  -o or-rbs.mir 2> print-after.err
```

`print-after.err` 开头（实测）：

```text
# *** IR Dump After RegBankSelect (regbankselect) ***:
# Machine code for function or_f: IsSSA, TracksLiveness, Legalized, RegBankSelected
bb.0 (%ir-block.0):
  %2:fpr(i32) = G_BITCAST %0:fpr(f32)
  %6:gpr(i32) = COPY %2:fpr(i32)
  %4:gpr(i32) = G_OR %6:gpr, %7:gpr
  $s0 = COPY %5:gpr(f32)
  RET_ReallyLR implicit $s0
```

这张 dump 给了三样 `-o` 文件不给的信息：

1. **函数属性行**：`IsSSA, TracksLiveness, Legalized, RegBankSelected`。
2. **use 侧 bank**：`G_BITCAST %0:fpr(f32)`、`G_OR %6:gpr, %7:gpr`。
3. **repair 方向**：`%6:gpr = COPY %2:fpr` 是 FPR→GPR，`$s0 = COPY %5:gpr` 是 GPR→FPR。

`-print-after` 也可以和 `-stop-after` 组合（实测）：`-o` 得到正常 MIR，stderr 得到 machine dump。

## 实验三：`_` 是“没有 bank”，不是“死了”

用一个人为的超宽类型把 RBS 逼失败：

```yaml
---
name:            add_s1024
legalized:       true
tracksRegLiveness: true
body:             |
  bb.0:
    %0:_(s1024) = G_CONSTANT i1024 1
    %1:_(s1024) = G_CONSTANT i1024 2
    %2:_(s1024) = G_ADD %0, %1
    RET_ReallyLR
```

`-global-isel-abort=1`（实测）：

```text
LLVM ERROR: unable to map instruction: %0:_(s1024) = G_CONSTANT i1024 1 (in function: add_s1024)
```

`-global-isel-abort=0` 的输出现象（实测）：

```yaml
legalized:       true
regBankSelected: true
selected:        false
failedISel:      true
registers:
  - { id: 0, class: _, preferred-register: '', flags: [  ] }
  - { id: 1, class: _, preferred-register: '', flags: [  ] }
  - { id: 2, class: _, preferred-register: '', flags: [  ] }
body:             |
  %0:_(s1024) = G_CONSTANT i1024 1
  %2:_(s1024) = G_ADD %0, %1
```

`-global-isel-abort=2 -pass-remarks-missed=gisel-regbankselect`（实测）：

```text
remark: <unknown>:0:0: unable to map instruction: %0:_(s1024) = G_CONSTANT i1024 1 (in function: add_s1024)
```

现在回答“怎么看 `_` 是死的还是没映射”：

| 现象 | 判断 | 依据 |
| --- | --- | --- |
| 死 def，但指令正常 | 仍然会被分配 bank | 实测：未使用的 `%3 = G_CONSTANT i32 9` 输出 `%3:gpr` |
| 活 vreg + `_` + `failedISel: true` | RBS 失败，没映射 | 实测 s1024 |
| 活 vreg + `_`，函数标 `regBankSelected` | 非法，verifier 会报 | 实测错误串 |
| `_` 只出现在 `DBG_VALUE`，且 vreg 无 def | 合法例外 | `MachineVerifier.cpp` 的 debug 条件 |

活 `_` 的 verifier 输出（实测）：

```text
*** Bad machine code: Generic virtual register must have a bank in a RegBankSelected function ***
- function:    unmapped_live
- instruction: %0:_(s32) = COPY $w0
```

结论：**先看 `failedISel`，再看 def/use，再看是否只在 debug 指令里。** 不要看到 `_` 就说“这是死 vreg”。

## 对比与取舍

| 需求 | 用什么 | 优点 | 代价 |
| --- | --- | --- | --- |
| 看整条 pipeline 的边界 | `-stop-after=regbankselect` | 最接近真实编译 | 包含 IRTranslator/Legalizer 差异 |
| 只比较 RBS | `-run-pass=regbankselect` | 输入固定，适合 A/B | 必须先有 legalized MIR |
| 看 use 侧 bank 与 repair 方向 | `-print-after` | 信息最全 | 写 stderr，输出吵 |
| 抓非法中间状态 | `-verify-machineinstrs` | 错误具体到 operand | 有运行开销 |
| 让失败显形 | `-global-isel-abort=1` | 直接 fatal，定位最快 | 不能继续跑后续 pass |
| 看失败但不中止 | `-global-isel-abort=2` + remarks | 保留 MIR | 需要记得开 remark |

## 踩雷集锦

1. **对整份 MIR 做 hash 比较**：`ModuleID`、块编号、`maxCallFrameSize` 都会变。改正：用 `Compare-Object` 只看 `body:` 之后。
2. **把 stderr 当失败**：PowerShell 会把 `llc` 的 stderr 包成 `NativeCommandError`，即使退出码 0；`-print-after` 本来就写 stderr。改正：显式 `2> file`。
3. **拿 IR 跑 `-run-pass`**：报 `run-pass is for .mir file only.`。改正：先 `-stop-after=legalizer`。
4. **看到 `regBankSelected: true` 就认为成功**：失败路径也会置它。改正：同时检查 `failedISel` 和活 vreg 的 bank。
5. **把 `_` 当死 vreg**：死 def 仍会被映射；`_` 表示没有 class/bank。改正：跑 `-run-pass=machineverifier`，或看 `failedISel`。

## 进阶

- **assertions build + `-debug-only=regbankselect`**：会打印 `Assign:`、`Evaluating mapping cost for:`、`New best:`、`Copy: ... to ...`、`Best Mapping:`（源码 `LLVM_DEBUG`，未在官方包实测，标 `guided`）。
- **只跑 verifier**：`llc -run-pass=machineverifier -verify-machineinstrs file.mir` 实测可用，适合把可疑 MIR 单独送检。
- **`-print-before` / `-print-after-all`**：前者看 pass 前状态，后者看每一步；输出量很大，先缩小到一个函数。
- **MIR fixture**：把 legalized MIR 存进测试目录，Ch 19 会把它变成 lit 测试。

## 动手练习

1. 用 `or.ll` 分别跑 `-stop-after=regbankselect` 和 `-run-pass=regbankselect`，用 `Compare-Object` 证明函数体相同、文件头不同。
2. 用 `-print-after=regbankselect` 抓 stderr，指出三条跨 bank COPY 的两侧 bank。
3. 复现 `add_s1024` 的 abort=0/1/2，记录 `regBankSelected`、`failedISel`、`class: _` 三个字段。
4. 手写“死 def + 活 vreg 无 bank”的 MIR，先用 `-run-pass=machineverifier` 看哪条被报错，再解释为什么死 def 不在其中。
5. （`guided`）在 assertions build 里打开 `-debug-only=regbankselect`，对照源码里的 `LLVM_DEBUG` 行，找出 `Copy:` 日志对应的 COPY。

## 本章重点整理

- `-stop-after` 从 IR 跑到 RBS；`-run-pass` 只从 MIR 重放 RBS，且只接受 MIR。
- `-print-after` 写 stderr，能看到 use 侧 bank；`-o` 文件只标 def。
- `regBankSelected: true` 不代表成功；失败时还会有 `failedISel: true` 和活 `_`。
- `_` 的含义是“没有 regclass、没有 bank”，不是“dead”。
- 死 def 仍会被分配 bank；活 `_` 会被 verifier 拒绝；debug-only 的无定义 `_` 是例外。
- `-global-isel-abort=1` 让失败 fatal，`=2` 发 remark，`=0` 静默留下失败状态。

## 自我检核

- [ ] 能说出 `-stop-after` 与 `-run-pass` 的输入、输出和适用场景
- [ ] 能解释为什么 `-print-after` 的输出不在 `-o` 文件里
- [ ] 能看着 `%0:gpr(i64)`、`%0:gpr64(i64)`、`%0:_(i64)` 说出当前阶段
- [ ] 能解释 `regBankSelected: true` 为什么不能证明成功
- [ ] 能区分死 def、活 `_`、debug-only `_` 三种情况
- [ ] 知道 `-global-isel-abort=0/1/2` 分别会发生什么

## 延伸阅读

- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：Machine Function 的 YAML 字段、`registers`、`body`、`-run-pass` 小节。
  - **学什么**：`class:`、`%0:gpr(i64)`、`liveins` 的语法；为什么 `_` 是合法输出。
  - **和本章的关联**：实验一/二的 MIR 读写都依赖这份语法。
- **[GlobalISel Pipeline — MachineVerifier](https://llvm.org/docs/GlobalISel/Pipeline.html#machineverifier)**
  - **读哪里**：`MachineVerifier` 一节，特别是 `regBankSelected` 函数的约束。
  - **学什么**：为什么“有 bank”是 RBS 之后的不变量。
  - **和本章的关联**：实验三的报错就是这条不变量。
- **[RegBankSelect.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h)**
  - **读哪里**：`getRequiredProperties`、`getSetProperties`、`getClearedProperties`。
  - **学什么**：pass manager 如何设置 `regBankSelected`，以及为什么失败也会设置。
  - **和本章的关联**：解释 `regBankSelected: true` + `failedISel: true` 的组合。
- **[MachineVerifier.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/MachineVerifier.cpp)**
  - **读哪里**：generic virtual register 的检查块，搜索 `must have a bank`。
  - **学什么**：debug value 的例外条件，以及 bank 最大尺寸检查。
  - **和本章的关联**：`_` 的合法/非法边界。
- **[TargetRegisterInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/TargetRegisterInfo.cpp)**
  - **读哪里**：`printRegClassOrBank`。
  - **学什么**：MIR printer 为什么输出 `_`，以及 class 优先于 bank 的顺序。
  - **和本章的关联**：`gpr` 与 `gpr64` 的区分就来自这里。

下一章把观察手段变成实验设计：同一份 legalized MIR 上，Fast 与 Greedy 到底差几条 COPY、差多少编译时间，以及 Greedy 什么时候反而更差。

→ [Ch 17 Fast vs Greedy 实验设计](./17-fast-vs-greedy-experiments.md)
